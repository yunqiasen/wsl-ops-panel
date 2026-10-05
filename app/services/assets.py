import hashlib
import logging
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from subprocess import CalledProcessError

import yaml

from app.models.assets import AssetSnapshot, DockerContainerSnapshot, PackageVersionInfo, RuntimeVersionInfo
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
from app.services.asset_descriptions import resolve_asset_description
from app.services.asset_policies import AssetPolicyService
from app.services.capabilities import enrich_asset_capabilities
from app.services.docker_versions import DockerVersionService
from app.services.endpoints import enrich_service_endpoints, extract_primary_public_port, format_ports_for_display
from app.services.package_metadata import PackageMetadataService
from app.services.source_links import build_source_links, normalize_git_remote_url
from app.services.state_store import PanelStateStore

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
]
SYSTEMD_SUPPORTED_ACTIONS = ['delete']
READONLY_CATEGORY_IDS = ('node', 'python', 'project', 'host', 'system', 'agent_cli', 'agent', 'remote')
DELETED_ASSETS_PATH = Path('data/deleted_assets.yaml')

DOCKER_DESCRIPTION_OVERRIDES = {
    '/home/div/1_Project_dir/AI/CLIProxyAPI': 'CPA CLIProxyAPI：把 ChatGPT、Claude、Gemini 等账号凭证代理成 OpenAI 兼容 API，并提供管理面板。',
    '/home/div/1_Project_dir/AI/new-api': 'New API：AI 模型聚合与分发网关，可统一管理模型、渠道、令牌和 OpenAI 兼容接口。',
    '/home/div/1_Project_dir/AI/sub2api': 'Sub2API：订阅到 API 的网关服务，提供后台、健康检查和 OpenAI 兼容接口。',
    '/home/div/1_Project_dir/regmail-2api/资源/openai-cpa': 'OpenAI-cpa 主实例：注册系统与 Codex Manager Web 控制台，当前对应 8128 入口。',
    '/home/div/1_Project_dir/regmail-2api/资源/openai-cpa-2': 'OpenAI-cpa-2 实例：第二套注册系统与 Codex Manager Web 控制台，预留 8129 入口。',
    '/home/div/1_Project_dir/regmail-2api/资源/openai-cpa-3': 'OpenAI-cpa-3 实例：第三套注册系统与 Codex Manager Web 控制台，预留 8130 入口。',
    '/home/div/1_Project_dir/regmail-2api/资源/openai-cpa-4': 'OpenAI-cpa-4 实例：第四套注册系统与 Codex Manager Web 控制台，预留 8131 入口。',
    '/home/div/1_Project_dir/regmail-2api/资源': 'OpenAI-cpa-5 实例：第五套注册系统与 Codex Manager Web 控制台，通过独立 compose 文件运行，当前对应 8132 入口。',
    '/home/div/1_Project_dir/regmail-2api/资源/Gpt-Agreement-Payment/gopay-deploy': 'GAP GoPay 支付实验栈：运行资格、短信、支付相关自动化 worker；当前不是网页入口服务。',
    '/home/div/1_Project_dir/regmail-2api/资源/freemail-proxy': 'Freemail 反代入口：Nginx 转发邮箱池相关服务，当前对应 8228 入口。',
    '/home/div/1_Project_dir/AI/image/image2api/deploy': 'image2api 后端依赖栈：运行 MySQL 与 Redis 等数据服务，支撑图片生成 API；不是直接给用户打开的网页。',
    '/home/div/1_Project_dir/AI/image/xinghai-studio-console/deploy': '星海控制台后端栈：运行 image2api server、Redis、MySQL 等后台组件，给管理控制台和 API 提供服务。',
    '/home/div/1_Project_dir/AI/image/xinghai-image-studio-ui': '星海图像工作台用户端 UI：面向普通用户的图片生成/查看前端页面。',
    '/home/div/1_Project_dir/AI/team-manage-refresh': 'Team Manage Refresh：ChatGPT Team 账号管理增强版，包含 Token 提取、自动刷新和 CPA 凭证导出能力。',
    '/home/div/1_Project_dir/Project/Xianyu/xianyu-auto-reply-fix': 'Xianyu Auto Reply：闲鱼智能客服系统，支持多账号管理、AI 自动回复、自动发货确认和 Web 管理后台。',
    '/home/div/1_Project_dir/Project/check-cx': 'Check CX：AI 模型 API 可用性与延迟监控面板，包含本地数据库和 REST 辅助容器。',
    '/home/div/1_Project_dir/Project/metapi': 'Metapi：AI API 聚合平台的元管理层和统一代理服务，用于管理并转发上游 API。',
}

DOCKER_NAME_OVERRIDES = {
    '/home/div/1_Project_dir/regmail-2api/资源': 'OpenAI-cpa-5',
    '/home/div/1_Project_dir/regmail-2api/资源/Gpt-Agreement-Payment/gopay-deploy': 'GAP GoPay 支付实验栈',
    '/home/div/1_Project_dir/AI/image/image2api/deploy': 'image2api 后端依赖栈',
    '/home/div/1_Project_dir/AI/image/xinghai-studio-console/deploy': '星海控制台后端栈',
}


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
        self._package_metadata_service = PackageMetadataService()
        self._state_store = PanelStateStore(Path(config_root))
        self._deleted_assets_path = self._state_store.path.parent / 'deleted_assets.yaml'

    def list_assets(self, category_id: str) -> list[AssetSnapshot]:
        snapshot = self._registry_service.snapshot
        assets = self._build_assets_for_category(snapshot, category_id)
        self._save_category_snapshot(category_id, assets)
        return assets

    def get_asset(self, object_id: str) -> AssetSnapshot | None:
        snapshot = self._registry_service.snapshot
        object_map = {obj.id: obj for obj in snapshot.objects}
        target = object_map.get(object_id)
        if target is not None:
            assets = self._build_assets_for_category(snapshot, target.category)
            self._save_category_snapshot(target.category, assets)
            for asset in assets:
                if asset.object_id == object_id:
                    return self._enrich_detail_asset(asset)
            return None

        if object_id.startswith('docker__'):
            docker_assets = self._build_assets_for_category(snapshot, 'docker')
            self._save_category_snapshot('docker', docker_assets)
            for asset in docker_assets:
                if asset.object_id == object_id:
                    return asset
            # Never resurrect a removed Docker target from a stale cached card.
            return None

        for category_id in READONLY_CATEGORY_IDS:
            assets = self._build_assets_for_category(snapshot, category_id)
            self._save_category_snapshot(category_id, assets)
            for asset in assets:
                if asset.object_id == object_id:
                    return self._enrich_detail_asset(asset)
        cached = self._state_store.get_asset_snapshot(object_id)
        return self._enrich_detail_asset(cached) if cached is not None else None

    def get_assets(self, object_ids: list[str], *, errors: dict[str, str] | None = None) -> dict[str, AssetSnapshot]:
        """Resolve one selection against at most one snapshot per category."""
        snapshot = self._registry_service.snapshot
        objects = {obj.id: obj for obj in snapshot.objects}
        pending = set(object_ids)
        result: dict[str, AssetSnapshot] = {}
        errors = errors if errors is not None else {}
        categories = []
        category_ids: dict[str, set[str]] = {}
        for object_id in object_ids:
            target = objects.get(object_id)
            category = target.category if target else object_id.split('__', 1)[0]
            category_ids.setdefault(category, set()).add(object_id)
            if category in {'docker', 'systemd', *READONLY_CATEGORY_IDS} and category not in categories:
                categories.append(category)
        for category in categories:
            try:
                assets = self._build_assets_for_category(snapshot, category)
            except Exception as exc:
                for object_id in category_ids[category]:
                    errors[object_id] = f'资产扫描失败 ({type(exc).__name__})'
                    pending.discard(object_id)
                continue
            self._save_category_snapshot(category, assets)
            for asset in assets:
                if asset.object_id in pending:
                    try:
                        result[asset.object_id] = self._enrich_detail_asset(asset) if asset.category != 'docker' else asset
                    except Exception as exc:
                        errors[asset.object_id] = f'资产详情读取失败 ({type(exc).__name__})'
                    pending.remove(asset.object_id)
        for object_id in pending:
            if object_id.startswith('docker__') or object_id in objects:
                continue
            try:
                cached = self._state_store.get_asset_snapshot(object_id)
                if cached is not None:
                    result[object_id] = self._enrich_detail_asset(cached)
            except Exception as exc:
                errors[object_id] = f'资产详情读取失败 ({type(exc).__name__})'
        return result

    def _save_category_snapshot(self, category_id: str, assets: list[AssetSnapshot]) -> None:
        try:
            self._state_store.replace_asset_snapshots(category_id, assets)
        except Exception as exc:  # pragma: no cover - cache failures must not break operations
            LOGGER.warning('failed to persist %s asset snapshot: %s', category_id, exc)

    def _build_assets_for_category(self, snapshot: RegistrySnapshot, category_id: str) -> list[AssetSnapshot]:
        if category_id == 'docker':
            return build_docker_asset_snapshots(
                snapshot,
                self._scan_docker_containers(),
                recipe_service=self._docker_recipe_service,
                version_service=self._docker_version_service,
                resolve_remote_versions=False,
                deleted_asset_ids=load_deleted_asset_ids(self._deleted_assets_path),
            )
        if category_id == 'systemd':
            scanned_units, scan_failed = self._scan_systemd_units()
            return build_systemd_asset_snapshots(snapshot, scanned_units, scan_failed=scan_failed)
        if category_id == 'node':
            return [enrich_asset_capabilities(self._policy_service.apply(asset)) for asset in self._scan_readonly_assets(self._node_scanner, 'node')]
        if category_id == 'python':
            return [enrich_asset_capabilities(self._policy_service.apply(asset)) for asset in self._scan_readonly_assets(self._python_scanner, 'python')]
        if category_id == 'project':
            return [self._enrich_project_asset(asset) for asset in self._scan_readonly_assets(self._project_scanner, 'project')]
        if category_id == 'host':
            return [enrich_asset_capabilities(asset) for asset in self._scan_readonly_assets(self._host_process_scanner, 'host')]
        if category_id == 'system':
            return [enrich_asset_capabilities(asset) for asset in self._scan_readonly_assets(self._system_infra_scanner, 'system')]
        if category_id == 'remote':
            return build_remote_assets()
        if category_id == 'agent':
            node_assets = self._scan_readonly_assets(self._node_scanner, 'node')
            return build_agent_assets(node_assets)
        if category_id == 'agent_cli':
            return []
        return []

    def _enrich_detail_asset(self, asset: AssetSnapshot) -> AssetSnapshot:
        if asset.category == 'node':
            package_info = self._package_metadata_service.get_node_metadata(asset.name).to_dict()
            metadata = {**asset.metadata, 'package_info': package_info}
            return asset.model_copy(update={'metadata': metadata})
        if asset.category == 'python':
            package_info = self._package_metadata_service.get_python_metadata(asset.name).to_dict()
            metadata = {**asset.metadata, 'package_info': package_info}
            return asset.model_copy(update={'metadata': metadata})
        return asset

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

    def _enrich_project_asset(self, asset: AssetSnapshot) -> AssetSnapshot:
        metadata = dict(asset.metadata)
        web_ui = metadata.get('web_ui')
        if isinstance(web_ui, dict):
            port = str(web_ui.get('port') or metadata.get('primary_public_port') or '').strip()
            if port:
                metadata.setdefault('ports', f'{port}/tcp')
                metadata.setdefault('display_ports', port)
                metadata.setdefault('primary_public_port', port)
                metadata.setdefault(
                    'service_endpoints',
                    [
                        {
                            'label': 'Web 入口',
                            'host_port': port,
                            'container_port': port,
                            'path': str(web_ui.get('url_path') or '/'),
                            'primary': True,
                        }
                    ],
                )
        configured_endpoints = metadata.get('service_endpoints') if isinstance(metadata.get('service_endpoints'), list) else None
        metadata = enrich_service_endpoints(
            metadata,
            asset_id=asset.object_id,
            asset_name=asset.name,
            configured_endpoints=configured_endpoints,
        )
        return enrich_asset_capabilities(asset.model_copy(update={'metadata': metadata}))




AGENT_CLIENTS = [
    {
        'id': 'codex',
        'name': 'Codex',
        'package': '@openai/codex',
        'paths': '~/.codex, ~/.codex/skills, ~/.codex/config.toml, AGENTS.md',
        'tools': 'skills / MCP / prompts / config',
    },
    {
        'id': 'claude',
        'name': 'Claude Code',
        'package': '@anthropic-ai/claude-code',
        'paths': '~/.claude, ~/.claude.json, CLAUDE.md',
        'tools': 'MCP / commands / agents / settings',
    },
    {
        'id': 'gemini',
        'name': 'Gemini CLI',
        'package': '@google/gemini-cli',
        'paths': '~/.gemini, GEMINI.md',
        'tools': 'settings / prompts / extensions',
    },
    {
        'id': 'opencode',
        'name': 'OpenCode',
        'package': None,
        'paths': '~/.config/opencode, ~/.local/share/opencode',
        'tools': 'config / providers / plugins',
    },
    {
        'id': 'openclaw',
        'name': 'OpenClaw',
        'package': '@qingchencloud/openclaw-zh',
        'paths': '~/.openclaw',
        'tools': 'config / workflow / tools',
    },
    {
        'id': 'hermes',
        'name': 'Hermes',
        'package': None,
        'paths': '~/.hermes',
        'tools': 'config / workflow / tools',
    },
]


def build_agent_assets(node_assets: list[AssetSnapshot]) -> list[AssetSnapshot]:
    node_by_name = {asset.name: asset for asset in node_assets}
    assets: list[AssetSnapshot] = []
    for client in AGENT_CLIENTS:
        package_name = client['package']
        package_asset = node_by_name.get(package_name) if isinstance(package_name, str) else None
        installed = package_asset is not None
        metadata = {
            'agent_client': client['id'],
            'package_name': package_name,
            'config_paths': client['paths'],
            'tooling_scope': client['tools'],
            'source_links': package_asset.metadata.get('source_links', {}) if package_asset is not None else {},
        }
        assets.append(
            enrich_asset_capabilities(
                AssetSnapshot(
                    object_id=f"agent__{client['id']}",
                    category='agent',
                    name=str(client['name']),
                    status='installed' if installed else 'not detected',
                    current_version=package_asset.current_version if package_asset is not None else None,
                    supports_actions=[],
                    metadata=metadata,
                    actionable=False,
                    blocked_reason='CLI 本体在 Node 分类更新；这里维护配置、Skill、MCP 和工作流',
                    managed_by='agent',
                )
            )
        )
    return assets


def build_remote_assets() -> list[AssetSnapshot]:
    return [
        enrich_asset_capabilities(
            AssetSnapshot(
                object_id='remote__nodes',
                category='remote',
                name='节点中心',
                status='ready',
                metadata={
                    'remote_kind': 'nodes',
                    'description': 'SSH 连接底座：保存设备、测试连接、后续给 Node / Python / Agent 远程任务使用。',
                    'entry_url': '/remote/nodes',
                },
            )
        ),
        enrich_asset_capabilities(
            AssetSnapshot(
                object_id='remote__config-sync',
                category='remote',
                name='配置同步中心',
                status='planned',
                metadata={
                    'remote_kind': 'config_sync',
                    'description': '系统配置、代理、Shell、Git、Agent 提示词和 MCP 的跨设备同步入口。第一批只提供结构说明。',
                    'entry_url': '/remote/config-sync',
                },
            )
        ),
    ]

def build_docker_asset_snapshots(
    registry_snapshot: RegistrySnapshot,
    containers: list[DockerContainerSnapshot],
    *,
    recipe_service: DockerRecipeService | None = None,
    version_service: DockerVersionService | None = None,
    resolve_remote_versions: bool = True,
    deleted_asset_ids: set[str] | None = None,
) -> list[AssetSnapshot]:
    docker_version_service = version_service or DockerVersionService()
    containers_by_dir: dict[str, list[DockerContainerSnapshot]] = defaultdict(list)
    containers_by_name = {container.name: container for container in containers}

    for container in containers:
        if container.compose_working_dir:
            containers_by_dir[container.compose_working_dir].append(container)

    assets: list[AssetSnapshot] = []
    managed_project_dirs: set[tuple[str, str | None]] = set()
    deleted_ids = deleted_asset_ids if deleted_asset_ids is not None else load_deleted_asset_ids()
    for obj in registry_snapshot.objects:
        if obj.category != 'docker' or not obj.enabled or obj.id in deleted_ids:
            continue

        object_containers = sorted(containers_by_dir.get(obj.config['project_dir'], []), key=lambda item: item.name)
        configured_primary = obj.config.get('primary_container')
        named_primary = containers_by_name.get(configured_primary) if configured_primary else None
        project = obj.config.get('compose_project') or (named_primary.compose_project if named_primary else None)
        if project:
            object_containers = [c for c in object_containers if c.compose_project == project]
        elif configured_primary or len({c.compose_project for c in object_containers}) > 1:
            object_containers = []
        primary = _select_primary_container(obj, object_containers, containers_by_name)
        project = project or (primary.compose_project if primary else None)
        if project or object_containers:
            managed_project_dirs.add((obj.config['project_dir'], project))
        status = primary.status if primary is not None else 'not running'
        recipe = recipe_service.get(obj.config.get('recipe_id')) if recipe_service is not None else None
        lifecycle_strategy = obj.config.get('lifecycle_strategy', 'compose_pull')
        runtime = docker_version_service.build_runtime_version_info(primary)
        project_path = Path(obj.config['project_dir'])
        git_info = _read_git_info(project_path)
        source_url = _resolve_docker_source_url(obj, project_path)
        label_source_url = primary.labels.get('org.opencontainers.image.source') if primary is not None else None
        source_links = build_source_links(
            git_remote_url=source_url,
            image_repository=_parse_image_repository(primary.image) if primary is not None else None,
            labels=primary.labels if primary is not None else {},
        )
        capabilities = _build_discovered_capabilities(
            project_path,
            image_repository=_parse_image_repository(primary.image) if primary is not None else None,
            git_remote_url=git_info.get('git_remote_url'),
        )
        raw_ports = primary.ports if primary is not None else None
        version_info = _build_docker_version_info(
            obj,
            primary,
            version_service=docker_version_service,
            recipe=recipe,
            lifecycle_strategy=lifecycle_strategy,
            resolve_remote_versions=resolve_remote_versions,
            source_url=label_source_url or source_url,
        )

        metadata = {
            'type': obj.type,
            'description': _resolve_docker_description(
                project_path,
                configured=obj.description,
                labels=primary.labels if primary is not None else {},
                image_repository=_parse_image_repository(primary.image) if primary is not None else None,
                compose_project=primary.compose_project if primary is not None else None,
                primary_container=primary.name if primary is not None else None,
            ),
            'project_dir': obj.config['project_dir'],
            'compose_file': obj.config['compose_file'],
            'primary_container': obj.config.get('primary_container'),
            'compose_service': obj.config.get('compose_service'),
            'recipe_id': recipe.id if recipe is not None else obj.config.get('recipe_id'),
            'lifecycle_strategy': lifecycle_strategy,
            'version_source': version_info.version_source or obj.config.get('version_source', 'registry_tags'),
            'available_versions': version_info.versions,
            'managed_services': version_info.managed_services,
            'ignored_services': version_info.ignored_services,
            'runtime': version_info.runtime.model_dump() if version_info.runtime is not None else None,
            'source_status': version_info.source_status,
            'error': version_info.error,
            'version_source_status': version_info.source_status,
            'image_repository': _parse_image_repository(primary.image) if primary is not None else None,
            'source_links': source_links,
            'runtime_image_tag': runtime.image_tag,
            'runtime_oci_version': runtime.oci_version,
            'runtime_oci_revision': runtime.oci_revision,
            'ports': raw_ports,
            'display_ports': format_ports_for_display(raw_ports),
            'primary_public_port': extract_primary_public_port(raw_ports),
            'capabilities': capabilities,
            **git_info,
        }
        metadata = enrich_service_endpoints(
            metadata,
            asset_id=obj.id,
            asset_name=obj.name,
            configured_endpoints=obj.config.get('endpoints', []),
        )

        assets.append(
            enrich_asset_capabilities(
                AssetSnapshot(
                    object_id=obj.id,
                    category=obj.category,
                    name=obj.name,
                    status=status,
                    current_version=version_info.current_version,
                    latest_version=version_info.latest_version,
                    supports_actions=DOCKER_SUPPORTED_ACTIONS.copy(),
                    metadata=metadata,
                    containers=object_containers,
                    primary_container_name=primary.name if primary is not None else None,
                )
            )
        )
    used_asset_ids = {obj.id for obj in registry_snapshot.objects}
    assets.extend(
        _build_runtime_discovered_docker_assets(
            containers_by_dir,
            managed_project_dirs,
            docker_version_service,
            deleted_ids,
            resolve_remote_versions=resolve_remote_versions,
            used_asset_ids=used_asset_ids,
        )
    )
    return assets


def load_deleted_asset_ids(path: Path | str = DELETED_ASSETS_PATH) -> set[str]:
    target = Path(path)
    if not target.exists():
        return set()
    payload = yaml.safe_load(target.read_text(encoding='utf-8')) or {}
    if not isinstance(payload, dict):
        return set()
    assets = payload.get('assets', [])
    if not isinstance(assets, list):
        return set()
    return {str(item) for item in assets if str(item)}


def _build_runtime_discovered_docker_assets(
    containers_by_dir: dict[str, list[DockerContainerSnapshot]],
    managed_project_dirs: set[tuple[str, str | None]],
    version_service: DockerVersionService,
    deleted_asset_ids: set[str],
    *,
    resolve_remote_versions: bool = True,
    used_asset_ids: set[str] | None = None,
) -> list[AssetSnapshot]:
    assets: list[AssetSnapshot] = []
    used_ids = set(used_asset_ids or set())
    groups = []
    for project_dir, project_containers in sorted(containers_by_dir.items()):
        by_project = defaultdict(list)
        for container in project_containers:
            by_project[container.compose_project or ''].append(container)
        for project, members in sorted(by_project.items()):
            groups.append((project_dir, project, members, len(by_project) > 1))
    for project_dir, project, project_containers, multiple in groups:
        if (project_dir, project or None) in managed_project_dirs or (project_dir, None) in managed_project_dirs:
            continue
        containers = sorted(project_containers, key=lambda item: item.name)
        primary = containers[0] if containers else None
        if primary is None:
            continue
        project_path = Path(project_dir)
        compose_file = primary.labels.get('com.docker.compose.project.config_files', '').split(',')[0] or _detect_compose_file(project_path)
        compose_service = primary.compose_service or primary.name
        image_repository = _parse_image_repository(primary.image)
        runtime = version_service.build_runtime_version_info(primary)
        git_info = _read_git_info(project_path)
        capabilities = _build_discovered_capabilities(project_path, image_repository=image_repository, git_remote_url=git_info.get('git_remote_url'))
        raw_ports = primary.ports
        name = _display_docker_project_name(project_path, primary)
        if multiple:
            name = f'{name} ({project})'
        object_id = discovered_docker_object_id(project_path, project)
        # Legacy display IDs have no reliable ownership. Do not alias them to a
        # newly discovered project or apply their tombstones to its siblings.
        if object_id in deleted_asset_ids or object_id in used_ids:
            continue
        used_ids.add(object_id)
        source_url = primary.labels.get('org.opencontainers.image.source') or git_info.get('git_remote_url')
        if resolve_remote_versions and image_repository:
            registry_kwargs = {'current_version': runtime.oci_version or primary.image_tag}
            if source_url:
                registry_kwargs['source_url'] = source_url
            version_info = version_service.get_registry_tag_version_info(
                image_repository,
                **registry_kwargs,
            )
        else:
            version_info = PackageVersionInfo(
                current_version=runtime.oci_version or primary.image_tag,
                versions=[],
                source_status='deferred',
                version_source='registry_tags' if image_repository else 'unknown',
            )
        metadata = {
            'type': 'docker_compose_discovered',
            'description': _resolve_docker_description(
                project_path,
                labels=primary.labels,
                image_repository=image_repository,
                compose_project=primary.compose_project,
                primary_container=primary.name,
            ),
            'discovery_source': 'runtime_discovered',
            'project_dir': project_dir,
            'compose_file': compose_file,
            'primary_container': primary.name,
            'compose_service': compose_service,
            'compose_project': primary.compose_project,
            'image_repository': image_repository,
            'source_links': build_source_links(
                git_remote_url=git_info.get('git_remote_url'),
                image_repository=image_repository,
                labels=primary.labels,
            ),
            'lifecycle_strategy': 'compose_pull',
            'version_source': version_info.version_source or ('registry_tags' if image_repository else 'unknown'),
            'available_versions': version_info.versions,
            'runtime': runtime.model_dump(),
            'source_status': version_info.source_status,
            'error': version_info.error,
            'version_source_status': version_info.source_status,
            'runtime_image_tag': runtime.image_tag,
            'runtime_oci_version': runtime.oci_version,
            'runtime_oci_revision': runtime.oci_revision,
            'ports': raw_ports,
            'display_ports': format_ports_for_display(raw_ports),
            'primary_public_port': extract_primary_public_port(raw_ports),
            'capabilities': capabilities,
            **git_info,
        }
        metadata = enrich_service_endpoints(metadata, asset_id=object_id, asset_name=name)

        assets.append(
            enrich_asset_capabilities(
                AssetSnapshot(
                    object_id=object_id,
                    category='docker',
                    name=name,
                    status=primary.status,
                    current_version=version_info.current_version,
                    latest_version=version_info.latest_version,
                    supports_actions=DOCKER_DISCOVERED_SUPPORTED_ACTIONS.copy(),
                    metadata=metadata,
                    containers=containers,
                    primary_container_name=primary.name,
                )
            )
        )
    return assets



def discovered_docker_object_id(project_dir: Path | str, project: str) -> str:
    """Identity depends on the directory and Compose project, never scan order."""
    path = Path(project_dir).resolve()
    identity = f'{path}\0{project}'
    suffix = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    return f'docker__{_slugify(path.name or project)}-{suffix}'


def _display_docker_project_name(project_path: Path, primary: DockerContainerSnapshot) -> str:
    override = DOCKER_NAME_OVERRIDES.get(str(project_path))
    if override:
        return override
    return project_path.name or primary.compose_project or primary.name


def _resolve_docker_description(
    project_path: Path,
    *,
    configured: str | None = None,
    labels: dict[str, str] | None = None,
    image_repository: str | None = None,
    compose_project: str | None = None,
    primary_container: str | None = None,
) -> str:
    override = DOCKER_DESCRIPTION_OVERRIDES.get(str(project_path))
    description = resolve_asset_description(project_path, configured=configured or override, labels=labels or {})
    if description:
        return description
    parts = [f'Docker Compose 自动发现项目：{project_path.name or compose_project or primary_container or "unknown"}。']
    if compose_project:
        parts.append(f'Compose 项目 {compose_project}。')
    if primary_container:
        parts.append(f'主容器 {primary_container}。')
    if image_repository:
        parts.append(f'镜像仓库 {image_repository}。')
    parts.append('未找到 README、package 描述或 OCI 描述，建议后续补充项目说明。')
    return ''.join(parts)


def _resolve_docker_source_url(obj: ObjectDefinition, project_path: Path) -> str | None:
    configured = obj.config.get('source_url')
    if isinstance(configured, str) and configured.strip():
        return normalize_git_remote_url(configured)
    git_info = _read_git_info(project_path)
    remote = git_info.get('git_remote_url')
    return normalize_git_remote_url(remote) if remote else None


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
        assets.append(
            enrich_asset_capabilities(
                AssetSnapshot(
                    object_id=obj.id,
                    category=obj.category,
                    name=obj.name,
                    status=status,
                    supports_actions=SYSTEMD_SUPPORTED_ACTIONS.copy(),
                    metadata=metadata,
                )
            )
        )
    return assets


def _select_primary_container(
    obj: ObjectDefinition,
    object_containers: list[DockerContainerSnapshot],
    containers_by_name: dict[str, DockerContainerSnapshot],
) -> DockerContainerSnapshot | None:
    configured_primary = obj.config.get('primary_container')
    if configured_primary:
        primary = containers_by_name.get(configured_primary)
        if primary is not None and primary in object_containers:
            return primary
        return None

    configured_service = obj.config.get('compose_service')
    if configured_service:
        for container in object_containers:
            if container.compose_service == configured_service:
                return container

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
    source_url: str | None = None,
) -> PackageVersionInfo:
    runtime = version_service.build_runtime_version_info(primary)
    managed_services = list(recipe.managed_services) if recipe is not None else list(obj.config.get('managed_services', []))
    ignored_services = list(recipe.ignored_services) if recipe is not None else list(obj.config.get('ignored_services', []))
    if lifecycle_strategy == 'compose_local_build_git_tag' and recipe is not None:
        runtime_current_version = _runtime_current_version(runtime)
        if not resolve_remote_versions:
            return PackageVersionInfo(
                current_version=runtime_current_version,
                runtime=runtime,
                lifecycle_strategy=lifecycle_strategy,
                version_source=obj.config.get('version_source', 'git_tags'),
                source_status='deferred',
                managed_services=managed_services,
                ignored_services=ignored_services,
            )
        info = version_service.get_git_tag_version_info(recipe.repo_dir, fetch=False)
        return info.model_copy(
            update={
                'current_version': info.current_version or runtime_current_version,
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
    registry_kwargs = {'current_version': runtime.oci_version or (primary.image_tag if primary is not None else None)}
    if source_url:
        registry_kwargs['source_url'] = source_url
    info = version_service.get_registry_tag_version_info(
        image_repository,
        **registry_kwargs,
    )
    return info.model_copy(
        update={
            'runtime': runtime,
            'lifecycle_strategy': lifecycle_strategy,
            'version_source': info.version_source or obj.config.get('version_source') or 'registry_tags',
            'managed_services': managed_services,
            'ignored_services': ignored_services,
        }
    )


def _runtime_current_version(runtime: RuntimeVersionInfo) -> str | None:
    if runtime.oci_version:
        return runtime.oci_version if runtime.oci_version.startswith('v') else f'v{runtime.oci_version}'
    return runtime.image_tag
